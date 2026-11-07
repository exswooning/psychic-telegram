"""A shortcut whose target copy this user cannot see (live: ward1@, account 3, 2026-11-02 -- one
shortcut answered 404 and FAILED the whole user). Every copy is tried; none reachable is a reasoned
skip that repair re-walks, never a failed user. Any other refusal still fails."""
import threading

import drive_engine
from db import bulk_seed_identities
from resilience import PermanentAPIError

SRC = "1" + "s" * 32
COPY_A, COPY_B = "1" + "a" * 32, "1" + "b" * 32


def _migrator(db, refuse):
    bulk_seed_identities(db, [("u1@src", "u1@tgt"), ("u2@src", "u2@tgt"), ("u3@src", "u3@tgt")])
    db.record_mapping("u2@src", SRC, COPY_A, "file")
    db.record_mapping("u3@src", SRC, COPY_B, "file")
    m = object.__new__(drive_engine.DriveMigrator)
    m.db, m.source_user, m.stats = db, "u1@src", {}
    m.settings = type("S", (), {"dry_run": False})()
    m._stats_lock = threading.Lock()
    tried = []

    class _Files:
        def create(self, body, **k):
            target = body["shortcutDetails"]["targetId"]
            tried.append(target)

            def run(_):
                if target in refuse:
                    raise refuse[target]
                return {"id": "SC1"}
            return type("R", (), {"execute": run})()
    m.tgt = type("T", (), {"files": lambda s: _Files()})()
    m._retry = lambda fn, **k: fn()
    m._pending_shortcuts = [({"id": "SHORT", "name": "Rates", "shortcutDetails": {"targetId": SRC}}, "PARENT")]
    return m, tried


def _status(db):
    return db.conn.execute("SELECT status, error_message FROM audit_log WHERE item_id='SHORT'").fetchone()


def test_an_unreachable_copy_falls_through_to_one_the_user_can_see(db):
    m, tried = _migrator(db, {COPY_A: PermanentAPIError(f"HTTP 404 (notFound): File not found: {COPY_A}")})
    m._fixup_shortcuts()
    assert tried == [COPY_A, COPY_B] and db.get_target_id("u1@src", "SHORT", "shortcut") == "SC1"
    assert _status(db)["status"] == "SUCCESS"


def test_no_reachable_copy_is_a_reasoned_skip_not_a_failure(db):
    m, tried = _migrator(db, {COPY_A: PermanentAPIError("HTTP 404 (notFound): File not found"),
                             COPY_B: PermanentAPIError("HTTP 404 (notFound): File not found")})
    m._fixup_shortcuts()
    row = _status(db)
    assert row["status"] == "SKIPPED_UNRESOLVED_TARGET" and "2 tried" in row["error_message"]
    assert m.stats.get("failed", 0) == 0


def test_any_other_refusal_still_fails(db):
    m, tried = _migrator(db, {COPY_A: PermanentAPIError("HTTP 403 (forbidden): no")})
    m._fixup_shortcuts()
    assert _status(db)["status"] == "FAILED" and tried == [COPY_A]
