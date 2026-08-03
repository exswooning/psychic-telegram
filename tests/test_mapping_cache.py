"""
tests/test_mapping_cache.py
===========================
The id_mapping read-through cache.

get_target_id runs before every mutating call -- once per file, per message,
per event, and again for every deferred shortcut -- so on a resumed run it is
the most frequent query in the system, and each one goes through a connection
the process-wide write lock is contending on.

The tests that matter here are not the hit-rate ones. They are the ones that
pin *read-through* rather than *snapshot*, because today one thread owns a
user and a snapshot would happen to stay correct. Under intra-user
concurrency it would not, and the failure would present as duplicated work
rather than as a crash -- which is the hardest kind to notice.
"""

from __future__ import annotations

import threading

import pytest

from db import MigrationDB, bulk_seed_identities

USER = "alice@tenanta.com"


@pytest.fixture
def ledger(tmp_path):
    db = MigrationDB(str(tmp_path / "m.db"))
    db.init_schema()
    return db


class TestItIsAReadThroughCacheNotASnapshot:
    def test_a_mapping_written_after_preload_is_visible(self, ledger):
        """The property the whole design turns on. A snapshot would answer
        None here, the caller would re-migrate, and the item would duplicate
        with nothing raising."""
        ledger.preload_mappings(USER)
        ledger.record_mapping(USER, "src-1", "tgt-1", "file")

        assert ledger.get_target_id(USER, "src-1", "file") == "tgt-1"

    def test_the_deferred_shortcut_case(self, ledger):
        """_fixup_shortcuts resolves targets at end of run for items migrated
        much earlier in the same run. A start-of-run snapshot misses every
        one of them."""
        ledger.preload_mappings(USER)
        for i in range(50):
            ledger.record_mapping(USER, f"src-{i}", f"tgt-{i}", "file")

        assert ledger.get_target_id(USER, "src-0", "file") == "tgt-0"
        assert ledger.get_target_id(USER, "src-49", "file") == "tgt-49"

    def test_concurrent_writers_on_one_user_all_become_visible(self, ledger):
        """What #6 will do. Every worker on a user must see what the others
        recorded, or they duplicate each other's work."""
        ledger.preload_mappings(USER)

        def worker(lo: int) -> None:
            for i in range(lo, lo + 25):
                ledger.record_mapping(USER, f"src-{i}", f"tgt-{i}", "file")

        threads = [threading.Thread(target=worker, args=(n * 25,))
                   for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        missing = [i for i in range(100)
                   if ledger.get_target_id(USER, f"src-{i}", "file") != f"tgt-{i}"]
        assert missing == [], f"{len(missing)} mappings invisible after write"

    def test_a_preload_racing_a_write_does_not_lose_it(self, ledger):
        """
        The actual race, not a sequential imitation of it.

        A record_mapping landing between preload's SELECT and its assignment
        into the cache must survive. The earlier version of this test called
        record and preload alternately on one thread, which passes against a
        wholesale `self._mapping_cache[user] = rows` -- the losing
        implementation -- because no write is ever in flight. Verified: it did
        pass against exactly that.

        So one thread writes continuously while another preloads repeatedly,
        and afterwards every write must be visible through the cache.
        """
        # The writer sets the duration and the preloader spins for as long as
        # it runs, rather than the other way round: a fixed number of preloads
        # finishes before the writer starts, and the race never happens. The
        # first version of this did exactly that and reported one write.
        WRITES = 300
        written: list[str] = []
        errors: list[BaseException] = []
        done = threading.Event()

        def writer():
            try:
                for i in range(WRITES):
                    key = f"race-{i}"
                    ledger.record_mapping(USER, key, f"tgt-{i}", "file")
                    written.append(key)
            except BaseException as exc:      # noqa: BLE001
                errors.append(exc)
            finally:
                done.set()

        t = threading.Thread(target=writer)
        t.start()
        preloads = 0
        while not done.is_set():
            ledger.preload_mappings(USER)
            preloads += 1
        t.join(timeout=30)

        assert not errors, f"writer died: {errors[0]!r}"
        assert len(written) == WRITES
        assert preloads > 5, f"only {preloads} preloads overlapped the writer"
        missing = [k for k in written
                   if ledger.get_target_id(USER, k, "file") is None]
        assert missing == [], (
            f"{len(missing)} of {len(written)} mappings were lost from the "
            f"cache by a concurrent preload, e.g. {missing[:3]}")

    def test_the_cache_never_answers_for_an_unpreloaded_user(self, ledger):
        """A miss is only meaningful when the map is known complete."""
        ledger.record_mapping("bob@tenanta.com", "src-1", "tgt-1", "file")
        assert ledger.get_target_id("bob@tenanta.com", "src-1", "file") == "tgt-1"

    def test_users_do_not_leak_into_each_other(self, ledger):
        """source_user scopes the key because two users may both own 'root'."""
        ledger.preload_mappings(USER)
        ledger.preload_mappings("bob@tenanta.com")
        ledger.record_mapping(USER, "shared-id", "alice-target", "file")

        assert ledger.get_target_id("bob@tenanta.com", "shared-id", "file") is None

    def test_item_types_do_not_collide(self, ledger):
        ledger.preload_mappings(USER)
        ledger.record_mapping(USER, "x", "as-file", "file")
        ledger.record_mapping(USER, "x", "as-folder", "folder")

        assert ledger.get_target_id(USER, "x", "file") == "as-file"
        assert ledger.get_target_id(USER, "x", "folder") == "as-folder"


class TestItAgreesWithTheDatabase:
    def test_a_preloaded_answer_matches_an_unpreloaded_one(self, ledger, tmp_path):
        """The cache must not become a second source of truth."""
        for i in range(20):
            ledger.record_mapping(USER, f"s{i}", f"t{i}", "file")
        ledger.preload_mappings(USER)

        fresh = MigrationDB(str(tmp_path / "m.db"))     # same file, no cache
        for i in range(20):
            assert (ledger.get_target_id(USER, f"s{i}", "file")
                    == fresh.get_target_id(USER, f"s{i}", "file"))

    def test_an_updated_mapping_is_not_stale_in_the_cache(self, ledger):
        ledger.preload_mappings(USER)
        ledger.record_mapping(USER, "src-1", "tgt-first", "file")
        ledger.record_mapping(USER, "src-1", "tgt-second", "file")

        assert ledger.get_target_id(USER, "src-1", "file") == "tgt-second"


class TestIdentityCache:
    """identity_map is written before a run and never during one, so a plain
    snapshot is safe here in a way it is not for id_mapping -- but it must
    still be invalidated by the commands that do write it."""

    def test_it_resolves_the_same_as_a_query(self, ledger):
        bulk_seed_identities(ledger, [("a@src.com", "a@tgt.com")])
        assert ledger.resolve_identity("a@src.com") == "a@tgt.com"
        assert ledger.resolve_identity("A@SRC.COM") == "a@tgt.com"
        assert ledger.resolve_identity("nobody@src.com") is None

    def test_seeding_after_a_read_invalidates_the_snapshot(self, ledger):
        """Without invalidation, provision-users followed by a migration in
        the same process would resolve every new account to None and drop
        every ACL naming them."""
        assert ledger.resolve_identity("late@src.com") is None    # caches
        bulk_seed_identities(ledger, [("late@src.com", "late@tgt.com")])

        assert ledger.resolve_identity("late@src.com") == "late@tgt.com"

    def test_an_empty_address_is_not_a_lookup(self, ledger):
        assert ledger.resolve_identity(None) is None
        assert ledger.resolve_identity("") is None
