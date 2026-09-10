"""
tests/test_scopes_narrow_by_purpose.py
======================================
Setup grants the union of everything, so a tenant is usable either way the
moment it is created. The moment a purpose is chosen the grant has to
narrow to it -- and for a migration that is not tidiness.

config.py states the property plainly: "the source side is deliberately
read-only: a source credential that cannot write is a structural guarantee,
not just a policy." A grant left wide after choosing migrate makes that
sentence false, silently, with nothing on screen to say so.

Measured on the live tenant before this existed, the source delegation held
25 scopes including https://mail.google.com/ (full Gmail, delete included),
admin.directory.user (create AND delete accounts),
admin.directory.user.security, admin.directory.group, apps.licensing,
gmail.settings.basic and chat.delete.
"""

from __future__ import annotations

import pytest

import config
import verify_scopes as v
from config import Settings


@pytest.fixture
def st():
    return Settings()


def short(scopes):
    return {s.replace("https://www.googleapis.com/auth/", "") for s in scopes}


class TestAMigrationCannotWriteToItsSource:
    def test_every_source_scope_is_read_only(self, st):
        for scope in v.scopes_for_purpose(st, "source", "migrate"):
            assert v._reads_only(scope), f"{scope} can write to the source"

    @pytest.mark.parametrize("dangerous", [
        "https://mail.google.com/",                                  # delete mail
        "https://www.googleapis.com/auth/admin.directory.user",      # delete accounts
        "https://www.googleapis.com/auth/admin.directory.group",
        "https://www.googleapis.com/auth/admin.directory.user.security",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.settings.basic",
        "https://www.googleapis.com/auth/chat.delete",
        "https://www.googleapis.com/auth/apps.licensing",
    ])
    def test_named_write_scopes_are_gone(self, st, dangerous):
        """Each of these was live on the source before this existed."""
        assert dangerous not in v.scopes_for_purpose(st, "source", "migrate")

    def test_the_declared_baseline_survives(self, st):
        """Narrowing must not break reading. config.SOURCE_SCOPES is what a
        migration actually needs."""
        got = v.scopes_for_purpose(st, "source", "migrate")
        for scope in config.SOURCE_SCOPES:
            assert scope in got, f"{scope} was removed and a migration needs it"

    def test_it_is_an_allowlist_not_a_subtraction(self):
        """A denylist has to be right about every scope that exists now and
        every one added later. Subtracting SEED_SCOPES alone left seven
        write scopes on the source."""
        import inspect

        src = inspect.getsource(v.scopes_for_purpose)
        assert "_reads_only" in src


class TestSeedingKeepsWhatItNeeds:
    def test_the_seed_grant_still_writes(self, st):
        seed = v.scopes_for_purpose(st, "source", "seed")
        assert "https://www.googleapis.com/auth/drive" in seed
        assert "https://www.googleapis.com/auth/gmail.insert" in seed

    def test_seeding_is_strictly_wider_than_migrating(self, st):
        seed = set(v.scopes_for_purpose(st, "source", "seed"))
        mig = set(v.scopes_for_purpose(st, "source", "migrate"))
        assert mig < seed, "migrate should be a strict subset of seed"

    def test_chat_seeding_survives(self, st):
        """Seeding chat needs spaces and messages -- the thing that took a
        day to get working."""
        seed = v.scopes_for_purpose(st, "source", "seed")
        assert "https://www.googleapis.com/auth/chat.spaces" in seed
        assert "https://www.googleapis.com/auth/chat.messages" in seed


class TestTheTargetIsNotNarrowed:
    def test_the_target_keeps_write_access_for_a_migration(self, st):
        """A migration writes to the target by definition. Narrowing it
        would break the thing the narrowing exists to protect."""
        tgt = v.scopes_for_purpose(st, "target", "migrate")
        assert "https://www.googleapis.com/auth/drive" in tgt
        assert "https://www.googleapis.com/auth/gmail.insert" in tgt

    def test_target_seed_and_migrate_agree(self, st):
        assert v.scopes_for_purpose(st, "target", "seed") \
            == v.scopes_for_purpose(st, "target", "migrate")


class TestTheInputIsChecked:
    def test_an_unknown_purpose_is_refused(self, st):
        with pytest.raises(ValueError, match="purpose"):
            v.scopes_for_purpose(st, "source", "whatever")


class TestReadsOnly:
    def test_readonly_suffix_passes(self):
        assert v._reads_only("https://www.googleapis.com/auth/drive.readonly")

    def test_anything_else_is_treated_as_a_write(self):
        """The safe direction to be wrong in: a missing read scope fails
        loudly on the first call, a surviving write scope fails silently
        until something uses it."""
        assert not v._reads_only("https://www.googleapis.com/auth/drive")
        assert not v._reads_only("https://mail.google.com/")

    def test_extras_are_empty_and_documented(self):
        """Anything added there needs a reason it cannot be read-only."""
        assert v.SOURCE_READ_EXTRAS == set()
