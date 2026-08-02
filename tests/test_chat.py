"""
tests/test_chat.py
==================
Chat is the one service where the engine knowingly loses something, so these
tests are mostly about pinning *which* thing is lost and proving the rest
survives.

The property that matters most is sender attribution. Replaying a whole
conversation as the migrating user would turn a five-person thread into one
person talking to themselves -- technically "migrated", practically useless.
"""

from __future__ import annotations

import pytest

from tests.conftest import SRC_USER, TGT_USER


@pytest.fixture
def chat_migrator(auth, db, settings, identity):
    import chat_engine

    settings.migrate_chat = True
    return chat_engine.ChatMigrator(auth, db, settings, SRC_USER, TGT_USER)


def _seed_conversation(auth, db):
    """A two-person thread, both participants mapped."""
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    src = auth.source_chat(SRC_USER)
    space = src.add_space("Deploys")
    src.add_chat_message(space, "Morning — did the deploy land?", SRC_USER)
    src.add_chat_message(space, "Yes, went out at 09:15.", "bob@tenanta.com")
    src.add_chat_message(space, "Great, closing the ticket.", SRC_USER)
    return space


def test_space_and_messages_are_migrated(chat_migrator, auth, db):
    _seed_conversation(auth, db)
    chat_migrator.run()

    tgt = auth.target_chat(TGT_USER)
    assert len(tgt.space_store) == 1
    space = next(iter(tgt.space_store.values()))
    assert space["displayName"] == "Deploys"
    assert chat_migrator.stats["spaces"] == 1
    assert chat_migrator.stats["messages"] == 3


def test_each_message_is_posted_as_its_original_sender(chat_migrator, auth, db):
    """The whole point: a group conversation must not collapse into a
    monologue by the migrating user."""
    _seed_conversation(auth, db)
    chat_migrator.run()

    posted = [m for msgs in auth.target_chat(TGT_USER).message_store.values()
              for m in msgs]
    senders = [m["sender"]["name"] for m in posted]
    assert senders.count(f"users/{TGT_USER}") == 2, "alice's two messages"
    assert senders.count("users/bob@tenantb.com") == 1, \
        "bob's message must be posted as bob, not as the migrating user"
    # More than one distinct sender is the property that matters: a thread
    # replayed entirely as one user is not a conversation any more.
    assert len(set(senders)) == 2


def test_import_mode_space_is_completed(chat_migrator, auth, db):
    """A space left in import mode is invisible to its members -- worse than
    never having created it."""
    _seed_conversation(auth, db)
    chat_migrator.run()

    space = next(iter(auth.target_chat(TGT_USER).space_store.values()))
    assert space["importMode"] is False
    assert auth.target_chat(TGT_USER).call_count("spaces.completeImport") == 1


def test_direct_messages_are_skipped(chat_migrator, auth, db):
    """A DM is defined by its participants, not a name; recreating it as a
    named space would quietly change what it is."""
    src = auth.source_chat(SRC_USER)
    dm = src.add_space("", space_type="DIRECT_MESSAGE")
    src.add_chat_message(dm, "hi", SRC_USER)

    chat_migrator.run()

    assert auth.target_chat(TGT_USER).space_store == {}
    row = db.get_audit(SRC_USER, dm, "chat_space")
    assert row is not None and row["status"] == "SKIPPED_NOT_A_SPACE"


def test_unmapped_sender_is_attributed_in_text_not_silently_reassigned(
    chat_migrator, auth, db
):
    """Someone with no identity_map entry cannot be impersonated. Their words
    must not appear under the migrating user's name with no indication."""
    src = auth.source_chat(SRC_USER)
    space = src.add_space("Vendors")
    src.add_chat_message(space, "Invoice attached.", "outsider@partner.com")

    chat_migrator.run()

    posted = [m for msgs in auth.target_chat(TGT_USER).message_store.values()
              for m in msgs]
    assert len(posted) == 1
    assert "originally from" in posted[0]["text"]
    assert "Invoice attached." in posted[0]["text"]
    assert chat_migrator.stats["unmapped_senders"] == 1


def test_rerun_does_not_duplicate_spaces_or_messages(chat_migrator, auth, db,
                                                     settings):
    import chat_engine

    _seed_conversation(auth, db)
    chat_migrator.run()
    spaces_after_first = len(auth.target_chat(TGT_USER).space_store)

    second = chat_engine.ChatMigrator(auth, db, settings, SRC_USER, TGT_USER)
    second.run()

    assert len(auth.target_chat(TGT_USER).space_store) == spaces_after_first
    assert second.stats["spaces"] == 0
    assert second.stats["messages"] == 0


def test_dry_run_creates_nothing(auth, db, settings, identity):
    import chat_engine

    settings.migrate_chat = True
    settings.dry_run = True
    _seed_conversation(auth, db)

    m = chat_engine.ChatMigrator(auth, db, settings, SRC_USER, TGT_USER)
    m.run()

    assert auth.target_chat(TGT_USER).space_store == {}
    assert auth.target_chat(TGT_USER).call_count("spaces.create") == 0


def test_historical_timestamps_are_not_attempted(chat_migrator, auth, db):
    """
    Setting createTime needs app auth with chat.import, which is rejected at
    token-mint (verified live). The fake rejects createTime for the same
    reason, so this test fails loudly if anyone "fixes" timestamps by passing
    it anyway rather than by solving the auth problem.
    """
    _seed_conversation(auth, db)
    chat_migrator.run()

    for kw in auth.target_chat(TGT_USER).calls_to("chat.messages.create"):
        assert "createTime" not in (kw.get("body") or {})
    assert chat_migrator.stats["failed"] == 0
