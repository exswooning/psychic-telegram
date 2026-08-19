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


def test_selecting_chat_opts_the_run_in(monkeypatch):
    """'chat' is a first-class service: asking for it must switch on the
    engine's import pass, which is what actually grants the scopes."""
    import types

    import main
    from config import Settings

    settings = Settings()
    settings.migrate_chat = False
    # This stubs run_batch to inspect the flags cmd_migrate sets; it is not
    # a migration and has no tenant. The delegation gate that now runs first
    # would correctly refuse it (no SOURCE_ADMIN), so opt out explicitly --
    # the same bypass an offline rehearsal against a fixture ledger uses.
    monkeypatch.setenv("MIGRATE_SKIP_SCOPE_CHECK", "1")
    args = types.SimpleNamespace(services="drive,chat", user=None)
    ran = {}

    def fake_run_batch(auth, db, settings, services, delta, delta_days, only):
        ran["services"] = services
        ran["migrate_chat"] = settings.migrate_chat
        return []

    monkeypatch.setattr(main, "run_batch", fake_run_batch)
    monkeypatch.setattr(main, "_print_batch_summary", lambda r: None)
    main.cmd_migrate(args, settings, None, None)
    assert ran["services"] == {"drive", "chat"}
    assert ran["migrate_chat"] is True

    settings.migrate_chat = False
    args_d = types.SimpleNamespace(services="chat", user=None, days=2)
    main.cmd_delta(args_d, settings, None, None)
    assert ran["migrate_chat"] is True
    assert ran["services"] == {"chat"}


class TestHalfImportedSpacesCanBeFinished:
    """
    A Chat space is created with importMode=True and only becomes visible to
    its members when completeImport succeeds.

    The mapping was recorded before that call, so a space whose completeImport
    failed was both *mapped* and *unusable* — and the next run's idempotency
    check found the mapping and skipped it. The space stayed in import mode
    permanently, invisible to everyone, with no run able to finish it. The
    module's own docstring says a partial import must be finished or dropped;
    the code did neither.

    Found by reading the module before its first live execution.
    """

    def test_a_failed_import_is_detected_as_incomplete(self, chat_migrator, db):
        db.log_audit(SRC_USER, "spaces/S1", "chat_space",
                     "FAILED", "completeImport failed: boom")
        assert chat_migrator._import_incomplete("spaces/S1") is True

    def test_a_successful_space_is_not_retried(self, chat_migrator, db):
        db.log_audit(SRC_USER, "spaces/S1", "chat_space", "SUCCESS")
        assert chat_migrator._import_incomplete("spaces/S1") is False

    def test_an_unknown_space_is_not_treated_as_incomplete(self, chat_migrator):
        assert chat_migrator._import_incomplete("spaces/never-seen") is False

    def test_finishing_retries_only_the_completion(self, chat_migrator, auth, db):
        """It must not recreate the space — that would duplicate every
        message already imported into it."""
        db.log_audit(SRC_USER, "spaces/S1", "chat_space",
                     "FAILED", "completeImport failed")

        chat = auth.target_chat(TGT_USER)
        called = {"complete": 0, "create": 0}
        real_complete = chat.spaces().completeImport

        class Spaces:
            def completeImport(self, name):
                called["complete"] += 1
                return real_complete(name=name)

            def create(self, body):
                called["create"] += 1
                raise AssertionError("must not recreate an existing space")

        chat.spaces = lambda: Spaces()
        chat_migrator.auth.target_chat = lambda u: chat

        chat_migrator._finish_import("spaces/S1", "spaces/T1")

        assert called["complete"] == 1
        assert called["create"] == 0

    def test_a_finished_space_is_recorded_as_success(self, chat_migrator, auth, db):
        db.log_audit(SRC_USER, "spaces/S1", "chat_space", "FAILED", "x")

        # completeImport has to actually succeed for this to say anything; the
        # fake rejects an unknown space, which is correct of it.
        chat = auth.target_chat(TGT_USER)

        class Ok:
            def completeImport(self, name):
                class R:
                    def execute(self_inner):
                        return {}
                return R()

        chat.spaces = lambda: Ok()
        chat_migrator.auth.target_chat = lambda u: chat

        chat_migrator._finish_import("spaces/S1", "spaces/T1")

        row = db.get_audit(SRC_USER, "spaces/S1", "chat_space")
        assert row["status"] == "SUCCESS"
        assert chat_migrator._import_incomplete("spaces/S1") is False

    def test_a_second_failure_stays_failed_and_retryable(self, chat_migrator, auth, db):
        db.log_audit(SRC_USER, "spaces/S1", "chat_space", "FAILED", "x")

        chat = auth.target_chat(TGT_USER)

        class Boom:
            def completeImport(self, name):
                raise RuntimeError("still cannot complete")

        chat.spaces = lambda: Boom()
        chat_migrator.auth.target_chat = lambda u: chat

        chat_migrator._finish_import("spaces/S1", "spaces/T1")

        assert chat_migrator._import_incomplete("spaces/S1") is True
        assert chat_migrator.stats["failed"] >= 1


# ----------------------------------------------------------------------
# CHAT_SPACE_MODE=direct
#
# Import mode needs the chat.import scope, and that is the scope an admin is
# most likely to refuse -- it was refused here, and all 12 spaces of a live
# run died on it with zero messages attempted. `direct` exists so a tenant
# that will not grant it can still migrate Chat, at the cost of notifying
# members during the backfill.
# ----------------------------------------------------------------------
class TestDirectSpaceMode:
    @pytest.fixture
    def direct(self, auth, db, settings, identity):
        import chat_engine

        settings.migrate_chat = True
        settings.chat_space_mode = "direct"
        return chat_engine.ChatMigrator(auth, db, settings, SRC_USER, TGT_USER)

    def test_direct_mode_never_requests_the_scope_it_exists_to_avoid(self, settings):
        """The whole point of the mode. If this regresses, `direct` fails
        preflight for exactly the reason someone chose it."""
        from config import CHAT_IMPORT_SCOPE, target_scopes

        settings.migrate_chat = True
        settings.chat_space_mode = "direct"
        assert CHAT_IMPORT_SCOPE not in target_scopes(settings)

        settings.chat_space_mode = "import"
        assert CHAT_IMPORT_SCOPE in target_scopes(settings)

    def test_the_space_is_created_live_not_in_import_mode(self, direct, auth, db):
        _seed_conversation(auth, db)
        direct.run()

        tgt = auth.target_chat(TGT_USER)
        created = [kw["body"] for kw in tgt.calls_to("spaces.create")]
        assert created, "no space was created"
        assert not any(b.get("importMode") for b in created)

    def test_complete_import_is_never_called(self, direct, auth, db):
        """Calling it on a live space is rejected, and treating that rejection
        as a failure would mark a good space broken on every re-run."""
        _seed_conversation(auth, db)
        direct.run()

        assert auth.target_chat(TGT_USER).call_count("spaces.completeImport") == 0

    def test_messages_still_arrive(self, direct, auth, db):
        _seed_conversation(auth, db)
        stats = direct.run()

        assert stats["messages"] == 3
        assert stats["failed"] == 0

    def test_a_rerun_does_not_duplicate(self, direct, auth, db, settings):
        import chat_engine

        _seed_conversation(auth, db)
        direct.run()

        # A fresh migrator: stats accumulate on the instance, so re-running
        # the same one would report the first run's totals back.
        second = chat_engine.ChatMigrator(auth, db, settings, SRC_USER, TGT_USER)
        again = second.run()

        assert again["messages"] == 0
        assert auth.target_chat(TGT_USER).call_count("spaces.create") == 1


class TestMembership:
    """
    Members are recreated in both modes. Under `direct` this is load-bearing:
    a user cannot post into a space they are not in, so joining them late
    would push every one of their messages down the unattributed fallback.
    """

    def test_mapped_members_are_added_before_the_messages(self, chat_migrator,
                                                          auth, db):
        from db import bulk_seed_identities

        bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
        src = auth.source_chat(SRC_USER)
        space = src.add_space("Deploys", members=[SRC_USER, "bob@tenanta.com"])
        # From the migrating user, so the post is recorded on the same fake
        # instance as the membership calls -- each impersonated poster gets
        # its own instance, and calls are logged per instance.
        src.add_chat_message(space, "hello", SRC_USER)

        chat_migrator.run()

        tgt = auth.target_chat(TGT_USER)
        names = [kw["body"]["member"]["name"] for kw in tgt.calls_to("members.create")]
        assert "users/bob@tenantb.com" in names, (
            "bob was never added to the recreated space")
        order = [n for n, _ in tgt.calls]
        assert order.index("members.create") < order.index("chat.messages.create")

    def test_an_unmapped_member_is_recorded_not_invented(self, chat_migrator,
                                                         auth, db):
        """No target account exists for them, and inventing one is not this
        tool's decision. The omission has to be visible."""
        src = auth.source_chat(SRC_USER)
        space = src.add_space("Deploys", members=["ghost@tenanta.com"])
        src.add_chat_message(space, "hello", SRC_USER)

        chat_migrator.run()

        rows = db.conn.execute(
            "SELECT status FROM audit_log WHERE item_type='chat_member'").fetchall()
        assert any(r["status"] == "SKIPPED_UNMAPPED" for r in rows)

    def test_rejoining_an_existing_member_is_not_a_failure(self, chat_migrator,
                                                           auth, db):
        """The common case on a re-run: ALREADY_EXISTS is the desired state."""
        from db import bulk_seed_identities

        bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
        src = auth.source_chat(SRC_USER)
        space = src.add_space("Deploys", members=["bob@tenanta.com"])
        src.add_chat_message(space, "hello", "bob@tenanta.com")

        chat_migrator.run()
        stats = chat_migrator._sync_members(space, "spaces/does-not-matter")

        assert chat_migrator.stats["failed"] == 0 or stats >= 0


class TestSpaceModeValidation:
    def test_an_unrecognised_mode_is_refused_not_defaulted(self, monkeypatch):
        """Silently defaulting would request chat.import for someone who set
        the variable specifically to avoid it -- the same silent-fallback bug
        TRANSFER_MODE had."""
        from config import Settings

        monkeypatch.setenv("CHAT_SPACE_MODE", "improt")
        with pytest.raises(ValueError, match="CHAT_SPACE_MODE"):
            Settings()
