"""One source file shared with many users is copied once per user (live, dharan: a file
owned outside the migration, 19 ward copies). Every one of those copies is a right
answer for a link or a shortcut; anything else is still a difference."""
import link_rewrite
import verify_sample as V
from db import bulk_seed_identities

SRC = "1" + "s" * 32
COPY_A, COPY_B, OTHER = "1" + "a" * 32, "1" + "b" * 32, "1" + "x" * 32


def _two_copies(db):
    bulk_seed_identities(db, [("u1@src", "u1@tgt"), ("u2@src", "u2@tgt")])
    db.record_mapping("u1@src", SRC, COPY_A, "file")
    db.record_mapping("u2@src", SRC, COPY_B, "file")


def test_every_copy_is_listed_in_the_order_made(db):
    _two_copies(db)
    assert db.targets_for_source_id(SRC) == [COPY_A, COPY_B]


def test_a_link_to_any_genuine_copy_reads_as_repointed(db):
    _two_copies(db)
    src = f"see https://drive.google.com/file/d/{SRC}/view".encode()
    for copy in (COPY_A, COPY_B):
        tgt = f"see https://drive.google.com/file/d/{copy}/view".encode()
        assert link_rewrite.rewrite_bytes(src, V.copy_lookup(db, tgt))[0] == tgt


def test_a_link_to_something_else_still_differs(db):
    _two_copies(db)
    src = f"see https://drive.google.com/file/d/{SRC}/view".encode()
    tgt = f"see https://drive.google.com/file/d/{OTHER}/view".encode()
    assert link_rewrite.rewrite_bytes(src, V.copy_lookup(db, tgt))[0] != tgt


def test_a_shortcut_to_a_colleagues_file_resolves_to_their_copy(db):
    """Live: ward7's shortcut to planning@'s file was left SKIPPED_UNRESOLVED_TARGET."""
    import drive_engine
    bulk_seed_identities(db, [("u1@src", "u1@tgt"), ("u2@src", "u2@tgt")])
    db.record_mapping("u2@src", SRC, COPY_B, "file")          # the colleague's copy
    m = object.__new__(drive_engine.DriveMigrator)
    m.db, m.source_user, m.stats = db, "u1@src", {}
    m.settings = type("S", (), {"dry_run": False})()
    import threading
    m._stats_lock = threading.Lock()
    made = []

    class _Files:
        def create(self, body, **k):
            made.append(body)
            return type("R", (), {"execute": lambda s: {"id": "SC1"}})()
    m.tgt = type("T", (), {"files": lambda s: _Files()})()
    m._retry = lambda fn, **k: fn()
    m._pending_shortcuts = [({"id": "SHORT", "name": "Rates", "shortcutDetails": {"targetId": SRC}}, "PARENT")]
    m._fixup_shortcuts()
    assert made and made[0]["shortcutDetails"] == {"targetId": COPY_B}
    assert db.get_target_id("u1@src", "SHORT", "shortcut") == "SC1"


def test_a_skipped_shortcut_is_retried_only_once_something_new_was_copied(db):
    import repair
    bulk_seed_identities(db, [("u1@src", "u1@tgt")])
    db.log_audit("u1@src", "SHORT", "shortcut", "SKIPPED_UNRESOLVED_TARGET", "shortcut target has not migrated")
    db.conn.execute("UPDATE audit_log SET timestamp='2026-10-19T13:30:54Z'")
    db.conn.commit()
    assert repair.unresolved_shortcut_users(db) == []                 # nothing copied since
    db.record_mapping("u2@src", SRC, COPY_B, "file")                  # a copy made later
    assert repair.unresolved_shortcut_users(db) == ["u1@src"]
    db.record_mapping("u1@src", "SHORT", "SC1", "shortcut")            # now resolved
    assert repair.unresolved_shortcut_users(db) == []
